import { useEffect } from "react";
import { AccountsDialog } from "@/components/app/AccountsDialog";
import { AddRepoDialog } from "@/components/app/AddRepoDialog";
import { AppSidebar } from "@/components/app/AppSidebar";
import { CommandPalette } from "@/components/app/CommandPalette";
import { InboxView } from "@/components/app/InboxView";
import { ReviewView } from "@/components/app/ReviewView";
import { RevisionView } from "@/components/app/RevisionView";
import { FilesView } from "@/components/app/FileView";
import { Opening, Welcome } from "@/components/app/Screens";
import { ImpactView, MapView, WalkthroughView } from "@/components/app/AnalysisViews";
import { ModeSwitch, Toolbar, type Mode } from "@/components/app/Toolbar";
import { WikiView } from "@/components/app/WikiView";
import { ScrollArea } from "@/components/ui/scroll-area";
import { SidebarProvider } from "@/components/ui/sidebar";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useSystemMode } from "@/lib/look";
import { flattenPages } from "@/lib/model";
import { StoreProvider, useStore } from "@/lib/store";

function PageScreen({ repo, page, mode, file }: { repo: string; page: string; mode: Mode; file?: string }) {
  const { repos, data, setPalette, go } = useStore();
  const entry = repos.find((r) => r.path === repo);
  const d = data[repo];
  if (!entry) return <Welcome />;
  if (entry.status !== "ready" || !d?.tree) {
    return (
      <>
        <Toolbar parts={[entry.name]} />
        <Opening repo={entry.status === "ready" && d?.error ? { ...entry, status: "failed", error: d.error } : entry} />
      </>
    );
  }
  const docs = d.tree.docs ?? [];
  const pages = flattenPages(d.tree.pages);
  const pageId =
    page || docs.find((doc) => doc.path === "README.md")?.id || pages.find((p) => p.id === "overview")?.id || pages[0]?.id || "";
  const title =
    docs.find((doc) => doc.id === pageId)?.title ?? pages.find((p) => p.id === pageId)?.title ?? pageId;
  const label = { page: "", map: "Map", impact: "Impact", walkthrough: "Walkthrough", files: "Files" }[mode];
  const isDoc = pageId.startsWith("doc--");
  // Files is about the repository, so it stays reachable from a doc page too.
  const modes: Mode[] = isDoc ? ["page", "files"] : ["page", "map", "impact", "walkthrough", "files"];
  const parts =
    mode === "files" ? [entry.name, "Files", ...(file ? file.split("/") : [])] : label ? [entry.name, title, label] : [entry.name, title];
  return (
    <>
      <Toolbar parts={parts} onAsk={() => setPalette({ open: true })}>
        <ModeSwitch mode={mode} modes={modes} go={(next) => go({ kind: next, repo, page: pageId })} />
      </Toolbar>
      {mode === "files" ? (
        <FilesView repo={repo} page={pageId} file={file} />
      ) : (
      <ScrollArea className="main-scroll" key={`${mode}:${pageId}`}>
        {mode === "page" && (
          <WikiView
            key={`${repo}:${pageId}`}
            repo={repo}
            pageId={pageId}
            onReview={(task) => go({ kind: "review", repo, task })}
          />
        )}
        {mode === "map" && <MapView repo={repo} pageId={pageId} />}
        {mode === "impact" && <ImpactView repo={repo} pageId={pageId} />}
        {mode === "walkthrough" && <WalkthroughView repo={repo} pageId={pageId} />}
      </ScrollArea>
      )}
    </>
  );
}

function ReviewScreen({ repo, task }: { repo: string; task: string }) {
  const { repos, data } = useStore();
  const entry = repos.find((r) => r.path === repo);
  if (!entry) return <Welcome />;
  if (entry.status !== "ready" || !data[repo]?.notes)
    return (
      <>
        <Toolbar parts={[entry.name]} />
        <Opening repo={entry} />
      </>
    );
  return (
    <>
      <Toolbar parts={[entry.name, "Review the change"]} />
      <ScrollArea className="main-scroll">
        <ReviewView key={task} repo={repo} taskId={task} />
      </ScrollArea>
    </>
  );
}

function Shell() {
  const { view, setPalette, palette, chooseFolder, openFromGitHub, setAccountsOpen } = useStore();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey)) return;
      const key = e.key.toLowerCase();
      if (key === "k") {
        e.preventDefault();
        setPalette({ open: !palette.open });
      } else if (key === "o" && e.shiftKey) {
        e.preventDefault();
        openFromGitHub();
      } else if (key === "o") {
        e.preventDefault();
        void chooseFolder();
      } else if (key === ",") {
        e.preventDefault();
        setAccountsOpen(true);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [palette.open, setPalette, chooseFolder, openFromGitHub, setAccountsOpen]);

  return (
    <div className="app-window">
      <SidebarProvider className="window-body" style={{ "--sidebar-width": "272px" } as React.CSSProperties}>
        <AppSidebar />
        <main className="window-main">
          {(view.kind === "page" ||
            view.kind === "map" ||
            view.kind === "impact" ||
            view.kind === "walkthrough" ||
            view.kind === "files") && (
            <PageScreen
              repo={view.repo}
              page={view.page}
              mode={view.kind}
              file={view.kind === "files" ? view.file : undefined}
            />
          )}
          {view.kind === "inbox" && (
            <>
              <Toolbar parts={["Inbox"]} />
              <ScrollArea className="main-scroll">
                <InboxView />
              </ScrollArea>
            </>
          )}
          {view.kind === "review" && <ReviewScreen repo={view.repo} task={view.task} />}
          {view.kind === "revision" && (
            <>
              <Toolbar parts={["Inbox", "Improve this passage"]} />
              <ScrollArea className="main-scroll">
                <RevisionView key={view.id} repo={view.repo} id={view.id} />
              </ScrollArea>
            </>
          )}
          {view.kind === "home" && (
            <>
              <Toolbar parts={["CodeNib"]} />
              <Welcome />
            </>
          )}
        </main>
      </SidebarProvider>
    </div>
  );
}

export default function App() {
  useSystemMode();
  return (
    <StoreProvider>
      <TooltipProvider delayDuration={400}>
        <Shell />
        <CommandPalette />
        <AddRepoDialog />
        <AccountsDialog />
        <Toaster position="bottom-right" />
      </TooltipProvider>
    </StoreProvider>
  );
}
