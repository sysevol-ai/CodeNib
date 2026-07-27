import { lazy, StrictMode, Suspense } from "react";
import { createRoot } from "react-dom/client";

import "@/app/globals.css";
import { routeSegments, useBrowserLocation } from "@/lib/router";

const AskPage = lazy(() => import("@/app/[repoId]/ask/page"));
const WikiPageView = lazy(() => import("@/app/[repoId]/page"));
const Landing = lazy(() => import("@/app/page"));

function RouteLoading() {
  return (
    <div className="route-loading" role="status">
      <img src="/codenib-icon.svg" alt="" />
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

  const repoId = segments[0];
  if (segments[1] === "ask") {
    const query = new URLSearchParams(location.search).get("q") ?? "";
    return <AskPage repoId={repoId} query={query} />;
  }
  return <WikiPageView repoId={repoId} />;
}

const root = document.getElementById("root");
if (!root) {
  throw new Error("CodeNib frontend root is missing");
}
createRoot(root).render(
  <StrictMode>
    <Suspense fallback={<RouteLoading />}>
      <App />
    </Suspense>
  </StrictMode>
);
