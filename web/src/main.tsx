import { lazy, StrictMode, Suspense, useEffect } from "react";
import { createRoot } from "react-dom/client";

import "@/app/globals.css";
import { githubPathInput } from "@/lib/githubPreview";
import { navigate, routeSegments, useBrowserLocation } from "@/lib/router";
import {
  assetUrl,
  guardHome,
  homeRepository,
  openHomeRepository,
  restoreStaticRoute,
} from "@/lib/runtime";
import { startExperienceEvents } from "@/lib/experience";

const AskPage = lazy(() => import("@/app/[repoId]/ask/page"));
const WikiPageView = lazy(() => import("@/app/[repoId]/page"));
const Landing = lazy(() => import("@/app/page"));
const GenerateWikiPage = lazy(() => import("@/app/generate/page"));
const SavedWikiPage = lazy(() => import("@/app/saved-wiki/page"));
const AddRepo = lazy(() => import("@/app/add-repo/page"));
const RepositoryRoute = lazy(() => import("@/components/RepositoryRoute"));
const ShareCardPage = lazy(() => import("@/app/share-card/page"));
const GuardCasesPage = lazy(() => import("@/app/guard/page"));

function RouteLoading() {
  return (
    <div className="route-loading" role="status">
      <img src={assetUrl("/codenib-icon.svg")} alt="" />
      <span>Loading CodeNib…</span>
    </div>
  );
}

// A published Wiki has no landing page. openHomeRepository() sends its root
// address to the Wiki before the first render; this covers a later in-app
// visit to the root, once App listens for navigation.
function OpenHomeRepository({ repoId }: { repoId: string }) {
  useEffect(() => {
    const { search, hash } = window.location;
    navigate(`/${encodeURIComponent(repoId)}/${search}${hash}`, true);
  }, [repoId]);
  return <RouteLoading />;
}

function App() {
  const location = useBrowserLocation();
  const segments = routeSegments(location.pathname);
  if (segments.length === 0) {
    const repo = new URLSearchParams(location.search).get("repo");
    if (repo) return <RepositoryRoute key={repo} input={repo} />;
    const home = homeRepository();
    if (home) return <OpenHomeRepository repoId={home} />;
    // The hosted demo opens on real guard cases (runtime `home: "guard"`);
    // a local Wiki keeps its home. Both pages also have their own routes.
    return guardHome() ? <GuardCasesPage /> : <Landing />;
  }
  if (segments[0] === "guard" && segments.length === 1) {
    return <GuardCasesPage />;
  }
  if (segments[0] === "wikis" && segments.length === 1) {
    return <Landing />;
  }
  if (segments[0] === "share-card" && segments.length <= 2) {
    return <ShareCardPage key={segments[1] ?? "site"} repoId={segments[1]} />;
  }
  if (segments[0] === "browse" && segments.length === 1) {
    return <Landing browse />;
  }

  // Dedicated entry routes precede the prepared Wiki repository id route.
  if (segments[0] === "preview" && segments.length === 3) {
    return (
      <GenerateWikiPage
        key={`${segments[1]}/${segments[2]}`}
        owner={segments[1]}
        name={segments[2]}
      />
    );
  }
  if (segments[0] === "wiki" && segments.length === 2) {
    return <SavedWikiPage key={segments[1]} id={segments[1]} />;
  }
  if (segments[0] === "add-repo") {
    return <AddRepo />;
  }

  // A GitHub URL pasted after the domain resolves like the entry form.
  const pastedGitHub = githubPathInput(segments);
  if (pastedGitHub) {
    return <RepositoryRoute key={pastedGitHub} input={pastedGitHub} />;
  }

  const repoId = segments[0];
  // /<repo>/page/<id>/ names a page by path so that a static host can serve
  // each page its own readable document. An in-page ?p= link still wins.
  if (segments.length === 3 && segments[1] === "page") {
    const pageId = new URLSearchParams(location.search).get("p") || segments[2];
    return (
      <WikiPageView key={`${repoId}:${pageId}`} repoId={repoId} initialPageId={pageId} />
    );
  }
  // Legacy Wiki ids retain /ask. GitHub owner/repo aliases include repos
  // named "ask"; GitHub owner names cannot contain the legacy '__' separator.
  if (segments[1] === "ask" && (repoId.includes("__") || segments.length !== 2)) {
    const query = new URLSearchParams(location.search).get("q") ?? "";
    return <AskPage repoId={repoId} query={query} />;
  }
  if (segments.length === 2) {
    return <RepositoryRoute key={segments.join("/")} input={segments.join("/")}
      page={new URLSearchParams(location.search).get("p") || undefined}
      askQuery={segments[1] === "ask" ? new URLSearchParams(location.search).get("q") || "" : undefined} />;
  }
  const pageId = new URLSearchParams(location.search).get("p") || "overview";
  return (
    <WikiPageView
      key={`${repoId}:${pageId}`}
      repoId={repoId}
      initialPageId={pageId}
    />
  );
}

restoreStaticRoute();
openHomeRepository();
startExperienceEvents();

const root = document.getElementById("root");
if (!root) {
  throw new Error("CodeNib frontend root is missing");
}
createRoot(root).render(
  <StrictMode>
    <Suspense fallback={<RouteLoading />}>
      <App />
    </Suspense>
  </StrictMode>,
);
