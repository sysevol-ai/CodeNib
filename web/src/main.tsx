import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import AskPage from "@/app/[repoId]/ask/page";
import WikiPageView from "@/app/[repoId]/page";
import Landing from "@/app/page";
import "@/app/globals.css";
import { routeSegments, useBrowserLocation } from "@/lib/router";

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
    <App />
  </StrictMode>
);
