// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { recordExperience } from "@/lib/experience";
import Header from "@/components/Header";
import Markdown from "@/components/Markdown";
import SystemArchitecture from "@/components/SystemArchitecture";
import SavedWikiMaps from "@/components/SavedWikiMaps";
import RepoPoster from "@/components/RepoPoster";
import WikiGenerationForm from "@/components/WikiGenerationForm";
import WikiGraphPane from "@/components/WikiGraphPane";
import WikiRunLog from "@/components/WikiRunLog";
import WikiStateLegend from "@/components/WikiStateLegend";
import WikiToc from "@/components/WikiToc";
import { extractJourney, splitWikiMarkdown } from "@/lib/wikiPresentation";
import {
  shouldWithholdWikiPage,
  isSourceCheckedWikiPage,
  type Citation,
  type RepoInfo,
  type WikiPage,
} from "@/lib/api";
import { AppLink } from "@/lib/router";
import { withBasePath } from "@/lib/runtime";
import { buildSourceGraph, describeSourceNode, SOURCE_HUB } from "@/lib/sourceMap";
import { buildWikiGraph, NEUTRAL_COLOR, type WikiGraphNode } from "@/lib/wikiGraph";
import { wikiActivity } from "@/lib/wikiActivity";
import { clock, PAGE_STAGES, runStarted, runTitle } from "@/lib/wikiRun";
import { useWikiWorkspace } from "@/lib/workspace";
import {
  allWikiPages,
  loadSavedWiki,
  loadSavedWikiPage,
  loadWikiModels,
  loadSavedWikiGraphs,
  wikiModelLabel,
  wikiStages,
  type SavedWikiGraphs,
  type WikiModelCatalog,
  type WikiPageState,
  recentWikis,
  publishWiki,
  stopWiki,
  type SavedWiki,
} from "@/lib/visitorWiki";
import "@/components/WikiRun.css";

const STATE_DETAIL: Record<WikiPageState, string> = {
  ready: "Ready · click to read",
  running: "Being written now",
  pending: "Planned · written after the chapters before it",
  needs_review: "Withheld · it did not pass source or reader checks",
};

/**
 * A Wiki a visitor generated, in the same workspace as a prepared Wiki: page
 * list, map and reader. While it is written, the map is the progress: folders
 * of the snapshot first, then chapters that fill in as each passes its checks.
 */
export default function SavedWikiPage({ id }: { id: string }) {
  const [wiki, setWiki] = useState<SavedWiki | null>(null);
  const [error, setError] = useState("");
  const [active, setActive] = useState(
    () => new URLSearchParams(window.location.search).get("p") || "overview",
  );
  const [page, setPage] = useState<WikiPage | null>(null);
  const [pageError, setPageError] = useState("");
  const [citation, setCitation] = useState<Citation | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [modelCatalog, setModelCatalog] = useState<WikiModelCatalog | null>(null);
  const [notice, setNotice] = useState("");
  const [stopping, setStopping] = useState(false);
  const [showResume, setShowResume] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [publicationError, setPublicationError] = useState("");
  const [statusConnected, setStatusConnected] = useState(false);
  const [tocOpen, setTocOpen] = useState(false);
  const [arrived, setArrived] = useState<Set<string>>(() => new Set());
  const [sawRunning, setSawRunning] = useState(false);
  const [, tick] = useState(0);
  const sourcePanel = useRef<HTMLDivElement>(null);
  const previousStates = useRef<Record<string, WikiPageState> | null>(null);
  const owner = recentWikis().find((item) => item.id === id);
  const { wideEnough, mapHidden, workspace, readerWidth, setMapHidden, resizeReader } = useWikiWorkspace();

  useEffect(() => {
    loadWikiModels().then(setModelCatalog).catch(() => {});
  }, []);

  useEffect(() => {
    const robots = document.createElement("meta");
    robots.name = "robots";
    robots.content = "noindex, nofollow";
    document.head.append(robots);
    return () => robots.remove();
  }, []);

  useEffect(() => {
    let cancelled = false;
    let settled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const value = await loadSavedWiki(id);
        settled = value.status === "complete" || value.status === "partial";
        if (!cancelled) {
          setWiki(value);
          setError("");
          setStatusConnected(true);
        }
      } catch (reason) {
        if (!cancelled) {
          setStatusConnected(false);
          setError(
            reason instanceof Error
              ? reason.message
              : "Could not load the saved Wiki.",
          );
        }
      } finally {
        if (!cancelled && !settled) timer = setTimeout(poll, 2000);
      }
    };
    void poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [id, refresh]);

  const running = wiki?.status === "running" || wiki?.status === "queued";
  const complete = wiki?.status === "complete";
  const ready = wiki?.page_states[active] === "ready";

  // A chapter that turns ready while the page is open blooms once on the map.
  useEffect(() => {
    if (!wiki) return;
    if (running) setSawRunning(true);
    const before = previousStates.current;
    if (before) {
      const fresh = Object.entries(wiki.page_states)
        .filter(([pageId, state]) => state === "ready" && before[pageId] !== "ready")
        .map(([pageId]) => pageId);
      if (fresh.length) setArrived((current) => new Set([...current, ...fresh]));
    }
    previousStates.current = wiki.page_states;
  }, [wiki, running]);

  useEffect(() => {
    let cancelled = false;
    setPage(null);
    setPageError("");
    setCitation(null);
    if (ready)
      loadSavedWikiPage(id, active)
        .then((value) => {
          if (!cancelled) {
            setPage(value);
            recordExperience("first_chapter_read");
          }
        })
        .catch((reason) => {
          if (!cancelled)
            setPageError(
              reason instanceof Error
                ? reason.message
                : "Could not load this chapter.",
            );
        });
    return () => {
      cancelled = true;
    };
  }, [id, active, ready, refresh]);

  useEffect(() => {
    if (!running) {
      setStopping(false);
      return;
    }
    const timer = setInterval(() => tick((value) => value + 1), 1000);
    return () => clearInterval(timer);
  }, [running]);

  // The recorded references between chapters are saved when a run finishes.
  const [overviewMaps, setOverviewMaps] = useState<SavedWikiGraphs | null>(null);
  const [mapsFailed, setMapsFailed] = useState(false);
  const settled = complete || wiki?.status === "partial";
  useEffect(() => {
    let cancelled = false;
    setOverviewMaps(null);
    setMapsFailed(false);
    if (settled)
      loadSavedWikiGraphs(id, "overview")
        .then((value) => {
          if (!cancelled) setOverviewMaps(value);
        })
        .catch(() => {
          if (!cancelled) setMapsFailed(true);
        });
    return () => {
      cancelled = true;
    };
  }, [id, settled]);

  const outlineKey = JSON.stringify(wiki?.pages ?? []);
  const areaMap = overviewMaps?.system_map ?? null;
  const chapterGraph = useMemo(() => buildWikiGraph(wiki?.pages ?? [], areaMap), [outlineKey, areaMap]);
  const treeKey = JSON.stringify(wiki?.source_tree ?? null);
  const repository = wiki?.repository || owner?.repository || "";
  const source = useMemo(
    () => buildSourceGraph(repository, wiki?.source_tree),
    [repository, treeKey],
  );

  const pages = allWikiPages(wiki?.pages || []);
  const states: Record<string, WikiPageState> = {};
  for (const item of pages) states[item.id] = wiki?.page_states[item.id] ?? "pending";
  const completed = pages.filter((item) => states[item.id] === "ready").length;
  const selected = pages.find((item) => item.id === active);
  const writing = running && wiki && PAGE_STAGES.has(wiki.stage)
    ? pages.find((item) => item.id === wiki.active_page)
    : undefined;
  const now = Date.now() / 1000;

  const architecture = isSourceCheckedWikiPage(page)
    ? page?.media_slots?.find(
        (slot) =>
          slot.render_contract?.adapter === "architecture" &&
          slot.render_contract.provenance === "architecture-plan",
      )
    : undefined;
  const prose = splitWikiMarkdown(page?.markdown || "");
  const showPoster =
    !workspace &&
    active === "overview" &&
    !!page &&
    !!wiki &&
    !shouldWithholdWikiPage(page) &&
    !!overviewMaps?.coverage?.available &&
    overviewMaps.system_map.areas.filter((area) => area.symbols > 0).length >= 2;
  const posterRepo = wiki
    ? ({
        id: wiki.repository,
        repo: wiki.repository,
        commit_short: wiki.commit.slice(0, 8),
        base_commit: wiki.commit,
        source_url: `https://github.com/${wiki.repository}`,
        file_count: wiki.source_files ?? 0,
      } as RepoInfo)
    : null;

  function pick(pageId: string) {
    setActive(pageId);
    setTocOpen(false);
    history.replaceState(
      null,
      "",
      `${withBasePath(`/wiki/${id}`)}?p=${encodeURIComponent(pageId)}`,
    );
  }
  function cite(index: number) {
    setCitation(page?.citations[index] || null);
    setTimeout(
      () =>
        sourcePanel.current?.scrollIntoView({
          behavior: "smooth",
          block: "nearest",
        }),
      0,
    );
  }
  async function stop() {
    if (!owner) return;
    try {
      await stopWiki(owner);
      setStopping(true);
    } catch {
      setError("Could not stop generation. Please try again.");
    }
  }
  async function togglePublication() {
    if (!owner || !wiki) return;
    setPublishing(true);
    setPublicationError("");
    try {
      const result = await publishWiki(owner, !wiki.published);
      setWiki((current) => (current ? { ...current, published: result.published } : current));
      setNotice(
        result.published
          ? "Published — everyone can now find this Wiki in Community."
          : "Removed from Community. Your share link still works.",
      );
    } catch (reason) {
      setPublicationError(reason instanceof Error ? reason.message : "Could not update publication.");
    } finally {
      setPublishing(false);
    }
  }

  // --- The map: folders before the plan, chapters after it ---
  // The Overview is the whole map rather than a node on it.
  const surveying = !!writing && !chapterGraph.nodes.some((node) => node.id === writing.id);
  const tree = wiki?.source_tree;
  const folders = tree ? tree.folders.length + tree.more.folders : 0;
  const showChapters = pages.length > 0;
  let idle: { title: string; detail: string } | undefined;
  if (wiki && running) {
    if (writing && surveying)
      idle = { title: `Writing “${writing.title}”`, detail: "It covers every chapter on this map, so it comes first" };
    else if (writing)
      idle = { title: `Writing “${writing.title}”`, detail: `${completed} of ${pages.length} ready · open a filled chapter to read it` };
    else if (showChapters)
      idle = { title: runTitle(wiki, completed), detail: `${completed} of ${pages.length} chapters ready` };
    else if (wiki.stage === "outline")
      idle = { title: "Planning the chapters", detail: tree ? `from ${tree.files.toLocaleString()} code files` : "from the source just read" };
    else if (tree)
      idle = { title: `${tree.files.toLocaleString()} code files in ${folders} ${folders === 1 ? "folder" : "folders"}`, detail: wikiStages[wiki.stage] || "Reading the source" };
    else idle = { title: wikiStages[wiki.stage] || "Starting", detail: "The snapshot’s folders appear here once it is read" };
  } else if (wiki?.status === "partial") {
    idle = {
      title: pages.length ? `${completed} of ${pages.length} chapters ready` : "No chapters saved",
      detail: "Generation is not running",
    };
  }
  const activity = wiki ? wikiActivity(wiki, Date.now() / 1000, statusConnected) : null;
  const still = !!activity?.running && !activity.animate;
  const hasReferences = chapterGraph.edges.some((edge) => edge.kind === "references");
  let mapNote: string | null = null;
  if (complete && showChapters) {
    if (mapsFailed) mapNote = "The recorded references could not be loaded.";
    else if (!overviewMaps) mapNote = "Loading recorded references…";
    else if (!overviewMaps.system_map?.available)
      mapNote = overviewMaps.coverage.note || "No code graph for this repository, so this map shows the outline only.";
    else if (!hasReferences) mapNote = "No references between these chapters were found in the index.";
  }
  const describeChapter = (node: WikiGraphNode) => ({
    title: node.title,
    detail: STATE_DETAIL[states[node.id] ?? "pending"],
  });
  const map = (embedded: boolean) =>
    showChapters ? (
      <WikiGraphPane
        graph={chapterGraph}
        activeId={active}
        hasReferences={hasReferences}
        note={mapNote}
        onPick={pick}
        onOpenReference={(link) => {
          const anchor = link.example.anchor;
          if (anchor?.file && wiki)
            window.open(
              `https://github.com/${wiki.repository}/blob/${wiki.commit}/${anchor.file}#L${anchor.line}`,
              "_blank",
              "noopener,noreferrer",
            );
        }}
        onHide={() => setMapHidden(true)}
        onResizeReader={embedded ? undefined : resizeReader}
        embedded={embedded}
        states={complete ? undefined : states}
        arrived={arrived}
        describe={complete ? undefined : describeChapter}
        live={sawRunning}
        still={still}
        surveying={surveying}
        idle={idle}
        legend={complete ? undefined : <WikiStateLegend references={hasReferences} />}
      />
    ) : (
      <WikiGraphPane
        graph={source.graph}
        activeId={SOURCE_HUB}
        hasReferences={false}
        note={null}
        onPick={() => {}}
        onOpenReference={() => {}}
        onHide={() => setMapHidden(true)}
        onResizeReader={embedded ? undefined : resizeReader}
        embedded={embedded}
        readonly
        live
        still={still}
        // The opened folder keeps turning until chapters take over the map.
        states={running ? { [SOURCE_HUB]: "running" } : undefined}
        idle={idle}
        describe={(node) => describeSourceNode(node, source.facts)}
        legend={<WikiStateLegend folders />}
      />
    );

  // --- The reader ---
  const stopControl =
    owner && running ? (
      stopping ? (
        <span className="run-stopping" role="status">Stopping after the current request…</span>
      ) : (
        <button type="button" className="btn-ghost" onClick={stop}>
          Stop generation
        </button>
      )
    ) : null;
  const showChapter = ready && !!page && !shouldWithholdWikiPage(page);
  const stoppedBlock = wiki?.status === "partial" && (
    <div className="run-stopped" role="alert">
      <strong>{wiki.message || "This run stopped before finishing."}</strong>
      <p>
        {completed === 0
          ? "No chapters were saved. Generation is no longer running."
          : "Ready chapters are saved. Generation is no longer running."}
        {wiki.calls === 0 && !wiki.unreported_call_cost && " No model calls were made."}
      </p>
      <p>
        {owner
          ? "Resolve the issue above, then retry using your OpenRouter key."
          : "The owner can retry from the browser that started this Wiki."}
      </p>
      <button type="button" className="btn-ghost" onClick={() => setRefresh((value) => value + 1)}>
        Refresh status
      </button>
    </div>
  );
  const resume = owner && (wiki?.status === "partial" || wiki?.stalled || (!wiki && error)) && (
    <div className="wiki-resume">
      {!showResume ? (
        <button type="button" className="wgf-submit" onClick={() => setShowResume(true)}>
          {completed === 0 ? "Retry generation" : "Continue unfinished chapters"}
        </button>
      ) : (
        <WikiGenerationForm
          attempt={owner}
          resume
          onStarted={() => {
            setShowResume(false);
            setRefresh((value) => value + 1);
          }}
        />
      )}
    </div>
  );
  let selectedNote = "";
  if (wiki && selected && !ready) {
    const state = states[selected.id];
    selectedNote =
      state === "needs_review"
        ? "This chapter did not pass source or reader checks. Its draft is withheld; the owner can resume to retry it."
        : state === "running"
          ? `“${selected.title}” is being written now. It opens here once it passes its source checks.`
          : running
            ? `“${selected.title}” is planned. It is written after the chapters before it; open any filled chapter meanwhile.`
            : "This chapter has not been completed. The owner can continue generation without rebuilding ready chapters.";
  }
  const stripColor = writing ? chapterGraph.colors.get(writing.id) ?? NEUTRAL_COLOR : NEUTRAL_COLOR;
  const started = wiki ? runStarted(wiki) : now;

  return (
    <>
      <Header
        center={
          <nav className="breadcrumb" aria-label="Breadcrumb">
            <button className="toc-toggle" aria-label="Toggle section list" onClick={() => setTocOpen((open) => !open)}>
              ☰
            </button>
            <span className="crumb-sep">/</span>
            <span className="crumb-repo mono">{repository || "Repository Wiki"}</span>
            {selected && <span className="crumb-sep">/</span>}
            {selected && <span className="crumb-page">{selected.title}</span>}
          </nav>
        }
      />
      <div
        className={`wiki-grid saved-wiki${workspace ? " is-workspace" : ""}`}
        style={workspace && readerWidth ? ({ "--reader-w": `${readerWidth}px` } as CSSProperties) : undefined}
      >
        {tocOpen && <div className="toc-scrim" onClick={() => setTocOpen(false)} aria-hidden />}
        <aside className={`wiki-toc own-rail ${tocOpen ? "open" : ""}${still ? " is-still" : ""}`} data-rail="left">
          <div className="rail-title">{repository || "Repository Wiki"}</div>
          <div className="rail-sub own-rail-sub">
            {wiki?.commit ? (
              <a
                className="mono"
                href={`https://github.com/${wiki.repository}/tree/${wiki.commit}`}
                target="_blank"
                rel="noopener noreferrer"
              >
                Snapshot {wiki.commit.slice(0, 8)} ↗
              </a>
            ) : (
              <span>Snapshot pending</span>
            )}
            {wiki && (
              <span className="own-rail-status">
                {complete
                  ? `${completed} ${completed === 1 ? "chapter" : "chapters"}`
                  : showChapters
                    ? `${completed} of ${pages.length} ready`
                    : running
                      ? "Chapters not planned yet"
                      : "No chapters saved"}
              </span>
            )}
          </div>
          {wideEnough && mapHidden && (
            <button type="button" className="wiki-map-show" onClick={() => setMapHidden(false)}>
              Show map
            </button>
          )}
          {showChapters ? (
            <WikiToc
              pages={wiki!.pages}
              activeId={active}
              onPick={pick}
              colors={chapterGraph.colors}
              states={complete ? undefined : states}
            />
          ) : (
            <p className="own-rail-empty">
              {!wiki ? "" : running ? "Chapters appear here, and on the map, once they are planned." : "No chapters were generated."}
            </p>
          )}
          {wiki && (
            <div className="own-rail-foot">
              {complete && (
                <p className="own-publication">
                  {wiki.published
                    ? "Published in Community"
                    : owner
                      ? "Saved in My Wikis · not listed in Community"
                      : "Shared by link · not listed in Community"}
                </p>
              )}
              {owner && complete && (
                <AppLink
                  className="own-rail-link"
                  href={`/preview/${wiki.repository.split("/").map(encodeURIComponent).join("/")}`}
                >
                  Generate a new version →
                </AppLink>
              )}
            </div>
          )}
        </aside>

        {workspace && wiki && map(false)}

        <main className="wiki-main" aria-busy={!wiki || (ready && !page && !pageError)}>
          <div className="wiki-content">
            {notice && (
              <p role="status" className="own-notice">
                {notice}
              </p>
            )}
            {!wiki && !error && (
              <p className="own-opening" role="status">
                Opening your saved Wiki…
              </p>
            )}
            {error && (
              <p role="alert" className="trial-error">
                {error}
              </p>
            )}
            {!wiki && resume}
            {wiki && ready ? (
              <>
                <details className={`run-strip${running ? " is-running" : ""}`}>
                  <summary>
                    <span
                      className={`run-strip-mark${running ? (still ? " is-running is-still" : " is-running") : ""}${
                        wiki.status === "partial" ? " is-paused" : ""
                      }`}
                      style={running ? ({ "--step": stripColor } as CSSProperties) : undefined}
                      aria-hidden="true"
                    />
                    <span className="run-strip-text">
                      <b>
                        {running
                          ? writing
                            ? `Writing “${writing.title}”`
                            : runTitle(wiki, completed)
                          : complete
                            ? `${completed} chapters, all source-checked`
                            : runTitle(wiki, completed)}
                      </b>
                      {running && activity?.tone === "waiting" ? (
                        <span className="run-strip-wait">{activity.label}</span>
                      ) : (
                        <span>
                          {complete
                            ? `${wikiModelLabel(wiki.model ?? "anthropic/claude-sonnet-4.6", modelCatalog)} · $${wiki.reported_cost_usd.toFixed(4)} reported`
                            : `${completed} of ${pages.length} ready${running ? ` · ${clock(now - started)}` : ""}`}
                        </span>
                      )}
                    </span>
                    <span className="run-strip-toggle">{complete ? "How it was made" : running ? "Progress" : "Details"}</span>
                  </summary>
                  <WikiRunLog
                    wiki={wiki}
                    now={now}
                    connected={statusConnected}
                    colors={chapterGraph.colors}
                    catalog={modelCatalog}
                    onPick={pick}
                    heading={false}
                    actions={stopControl}
                  >
                    {stoppedBlock}
                    {resume}
                  </WikiRunLog>
                </details>
                {complete && owner && (
                  <section className="own-publish" aria-label="Community publication">
                    <div>
                      <h2>{wiki.published ? "Published in Community" : "Your Wiki is ready to share"}</h2>
                      <p>
                        {wiki.published
                          ? "Readers can discover this Wiki in Community. Your share link keeps working either way."
                          : "Saved in My Wikis. Publish it to Community so other readers can discover it. Until then, only people with its link can open it."}
                      </p>
                      {publicationError && (
                        <p role="alert" className="trial-error">
                          {publicationError}
                        </p>
                      )}
                    </div>
                    <div className="own-publish-actions">
                      {wiki.published && (
                        <AppLink className="own-rail-link" href="/browse?tab=community">
                          View in Community →
                        </AppLink>
                      )}
                      <button
                        type="button"
                        className={wiki.published ? "btn-ghost" : "wgf-submit"}
                        disabled={publishing}
                        onClick={togglePublication}
                      >
                        {publishing ? "Updating…" : wiki.published ? "Remove from Community" : "Publish to Community"}
                      </button>
                    </div>
                  </section>
                )}
                {pageError ? (
                  <p role="alert" className="trial-error">
                    {pageError}{" "}
                    <button type="button" onClick={() => setRefresh((value) => value + 1)}>
                      Retry loading
                    </button>
                  </p>
                ) : !page ? (
                  <div className="own-loading" role="status">
                    <span className="wgf-spinner" aria-hidden="true" /> Loading this chapter…
                  </div>
                ) : shouldWithholdWikiPage(page) ? (
                  <p className="own-selected-note">
                    This chapter did not pass source or reader checks. Its draft is withheld.
                  </p>
                ) : (
                  <>
                    {showPoster && overviewMaps && (
                      <RepoPoster
                        repoId={wiki.repository}
                        repo={posterRepo}
                        map={overviewMaps.system_map}
                        pages={wiki.pages}
                        journey={extractJourney(prose.body).journey}
                        lead={prose.lead.replace(/^#\s[^\n]*\n+/, "").split(/\n\s*\n/)[0]}
                        onPick={pick}
                        brandLabel={wiki.repository}
                        sourceRepoId={null}
                      />
                    )}
                    {!(showPoster && architecture) && (
                      <Markdown
                        citations={page.citations}
                        relations={page.evidence?.relations}
                        onCite={cite}
                        onPageLink={pick}
                        allowRemoteImages={false}
                        pageBasePath={`/wiki/${id}`}
                      >
                        {showPoster ? prose.body : architecture ? prose.lead : page.markdown}
                      </Markdown>
                    )}
                    {architecture && (
                      <>
                        <SystemArchitecture
                          key={`${id}:${active}`}
                          slot={architecture}
                          onEvidence={(ids) => {
                            const sources = ids.flatMap((value) =>
                              value.startsWith("R")
                                ? page.evidence?.relations.find((item) => item.id === value)?.anchors || []
                                : [value],
                            );
                            const index = sources
                              .map((value) => (/^E\d+$/.test(value) ? Number(value.slice(1)) - 1 : -1))
                              .find((value) => value >= 0 && value < page.citations.length);
                            if (index != null) cite(index);
                          }}
                        />
                        <Markdown
                          citations={page.citations}
                          relations={page.evidence?.relations}
                          onCite={cite}
                          onPageLink={pick}
                          allowRemoteImages={false}
                          pageBasePath={`/wiki/${id}`}
                        >
                          {prose.body}
                        </Markdown>
                      </>
                    )}
                    <div className="page-provenance source-checked">
                      <span>Evidence-linked Wiki chapter</span>
                      <span>{page.citations.length} source references</span>
                    </div>
                    {active === "overview" && !architecture && (
                      <p className="wiki-visual-unavailable" role="status">
                        The architecture diagram could not be validated. This chapter's source-checked text is
                        available; the diagram is missing.
                      </p>
                    )}
                    <SavedWikiMaps
                      hideSystemMap={showPoster || workspace}
                      id={id}
                      pageId={active}
                      repository={wiki.repository}
                      complete={!running}
                      onPick={pick}
                    />
                    <details className="saved-wiki-sources">
                      <summary>Sources for this chapter</summary>
                      {page.citations.map((item, index) => (
                        <button key={index} type="button" onClick={() => cite(index)}>
                          {item.file}:{item.start_line}–{item.end_line}
                        </button>
                      ))}
                    </details>
                  </>
                )}
              </>
            ) : wiki ? (
              <>
                {!workspace && <div className="own-inline-map">{map(true)}</div>}
                {selectedNote && (
                  <p
                    className={`own-selected-note is-${states[active] ?? "pending"}`}
                    style={{ "--step": chapterGraph.colors.get(active) ?? NEUTRAL_COLOR } as CSSProperties}
                  >
                    {selectedNote}
                  </p>
                )}
                <WikiRunLog
                  wiki={wiki}
                  now={now}
                  connected={statusConnected}
                  colors={chapterGraph.colors}
                  catalog={modelCatalog}
                  onPick={pick}
                  actions={stopControl}
                >
                  {stoppedBlock}
                  {resume}
                </WikiRunLog>
              </>
            ) : null}
            {citation && wiki && (
              <div className="saved-wiki-source" ref={sourcePanel}>
                <div>
                  <strong>
                    {citation.file}:{citation.start_line}–{citation.end_line}
                  </strong>
                  <button type="button" className="btn-ghost" aria-label="Close source" onClick={() => setCitation(null)}>
                    ×
                  </button>
                </div>
                <a
                  href={`https://github.com/${wiki.repository}/blob/${wiki.commit}/${(citation.file || "").split("/").map(encodeURIComponent).join("/")}${citation.start_line ? `#L${citation.start_line}${citation.end_line ? `-L${citation.end_line}` : ""}` : ""}`}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  Open pinned source on GitHub ↗
                </a>
                {citation.content && (
                  <pre>
                    <code>{citation.content}</code>
                  </pre>
                )}
              </div>
            )}
          </div>
        </main>
      </div>
    </>
  );
}
