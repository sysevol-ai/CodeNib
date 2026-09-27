import { lazy, StrictMode, Suspense } from "react";
import { createRoot } from "react-dom/client";

import "@/app/globals.css";
import { routeSegments, useBrowserLocation } from "@/lib/router";
import { assetUrl, restoreStaticRoute } from "@/lib/runtime";

const AskPage = lazy(() => import("@/app/[repoId]/ask/page"));
const WikiPageView = lazy(() => import("@/app/[repoId]/page"));
const Landing = lazy(() => import("@/app/page"));
const GenerateWikiPage = lazy(() => import("@/app/generate/page"));
const SavedWikiPage = lazy(() => import("@/app/saved-wiki/page"));
const AddRepo = lazy(() => import("@/app/add-repo/page"));

function RouteLoading() {
  return (
    <div className="route-loading" role="status">
      <img src={assetUrl("/codenib-icon.svg")} alt="" />
      <span>Loading CodeNib…</span>
    </div>
  );
}

function App() {
  const location = useBrowserLocation();
  const segments = routeSegments(location.pathname);
  if (segments.length === 0) {
    return <Landing />;
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

  const repoId = segments[0];
  if (segments[1] === "ask") {
    const query = new URLSearchParams(location.search).get("q") ?? "";
    return <AskPage repoId={repoId} query={query} />;
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
