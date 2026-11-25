import { useEffect } from "react";
import { AddRepoDialog } from "@/components/app/AddRepoDialog";
import { AppSidebar } from "@/components/app/AppSidebar";
import { CommandPalette } from "@/components/app/CommandPalette";
import { InboxView } from "@/components/app/InboxView";
import { ReviewView } from "@/components/app/ReviewView";
import { Opening, Welcome } from "@/components/app/Screens";
import { Toolbar } from "@/components/app/Toolbar";
import { WikiView } from "@/components/app/WikiView";
import { ScrollArea } from "@/components/ui/scroll-area";
import { SidebarProvider } from "@/components/ui/sidebar";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useSystemMode } from "@/lib/look";
import { flattenPages } from "@/lib/model";
import { StoreProvider, useStore } from "@/lib/store";

function PageScreen({ repo, page }: { repo: string; page: string }) {
  const { repos, data, setPalette, go } = useStore();
  const entry = repos.find((r) => r.path === repo);
  const d = data[repo];
  if (!entry) return <Welcome />;
  if (entry.status !== "ready" || !d?.tree) {
    if (entry.status === "ready" && d?.error) {
      return <Opening repo={{ ...entry, status: "failed", error: d.error }} />;
    }
    return <Opening repo={entry} />;
  }
  const docs = d.tree.docs ?? [];
  const pages = flattenPages(d.tree.pages);
  const pageId =
    page || docs.find((doc) => doc.path === "README.md")?.id || pages.find((p) => p.id === "overview")?.id || pages[0]?.id || "";
  const title =
    docs.find((doc) => doc.id === pageId)?.title ?? pages.find((p) => p.id === pageId)?.title ?? pageId;
  return (
    <>
      <Toolbar parts={[entry.name, title]} onAsk={() => setPalette({ open: true })} />
      <ScrollArea className="main-scroll">
        <WikiView
          key={`${repo}:${pageId}`}
          repo={repo}
          pageId={pageId}
          onReview={(task) => go({ kind: "review", repo, task })}
        />
      </ScrollArea>
    </>
  );
}

function ReviewScreen({ repo, task }: { repo: string; task: string }) {
  const { repos, data } = useStore();
  const entry = repos.find((r) => r.path === repo);
  if (!entry) return <Welcome />;
  if (entry.status !== "ready" || !data[repo]?.notes) return <Opening repo={entry} />;
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
  const { view, setPalette, palette, chooseFolder } = useStore();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey)) return;
      const key = e.key.toLowerCase();
      if (key === "k") {
        e.preventDefault();
        setPalette({ open: !palette.open });
      } else if (key === "o") {
        e.preventDefault();
        void chooseFolder();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [palette.open, setPalette, chooseFolder]);

  return (
    <div className="app-window">
      <SidebarProvider className="window-body" style={{ "--sidebar-width": "272px" } as React.CSSProperties}>
        <AppSidebar />
        <main className="window-main">
          {view.kind === "page" && <PageScreen repo={view.repo} page={view.page} />}
          {view.kind === "inbox" && (
            <>
              <Toolbar parts={["Inbox"]} />
              <ScrollArea className="main-scroll">
                <InboxView />
              </ScrollArea>
            </>
          )}
          {view.kind === "review" && <ReviewScreen repo={view.repo} task={view.task} />}
          {(view.kind === "home" || view.kind === "map" || view.kind === "impact" || view.kind === "walkthrough") && (
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
        <Toaster position="bottom-right" />
      </TooltipProvider>
    </StoreProvider>
  );
}
