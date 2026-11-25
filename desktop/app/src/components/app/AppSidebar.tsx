import { ChevronRight, ChevronsUpDown, FileText, FolderGit2, Inbox, MoreHorizontal, Plus, RotateCw, Search } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Kbd } from "@/components/ui/kbd";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuAction,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarMenuSub,
  SidebarMenuSubButton,
  SidebarMenuSubItem,
} from "@/components/ui/sidebar";
import { backend, message, type RepoView } from "@/lib/backend";
import { areaPages, hueFor } from "@/lib/areas";
import { flattenPages, taskNeed, taskTitle, type Task } from "@/lib/model";
import { useStore } from "@/lib/store";
import { NibMark } from "./NibMark";
import { StatusMark } from "./StatusMark";

function RepoActions({ repo }: { repo: RepoView }) {
  const { removeRepository, restart } = useStore();
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <SidebarMenuAction showOnHover aria-label={`More for ${repo.name}`}>
          <MoreHorizontal />
        </SidebarMenuAction>
      </DropdownMenuTrigger>
      <DropdownMenuContent side="right" align="start">
        <DropdownMenuItem onSelect={() => backend.showInFinder(repo.path).catch((e) => toast.error(message(e)))}>
          Show in Finder
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => restart(repo.path).catch((e) => toast.error(message(e)))}>
          Restart CodeNib for it
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => removeRepository(repo.path).catch((e) => toast.error(message(e)))}>
          Remove from CodeNib
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** The mark beside a repository: failed, working, or how many need you. */
function repoMark(repo: RepoView, tasks: Task[], needs: number) {
  if (repo.status === "failed") return <StatusMark status="failed" />;
  if (repo.status === "starting" || tasks.some((t) => t.status === "queued" || t.status === "running"))
    return <StatusMark status="working" />;
  if (needs > 0)
    return (
      <span className="sb-count" aria-label={`${needs} need you`}>
        <StatusMark status="needs" />
        {needs}
      </span>
    );
  return null;
}

function RepoTree({ repo }: { repo: RepoView }) {
  const { data, view, go, open, lines, restart } = useStore();
  const d = data[repo.path];
  const isHere = "repo" in view && view.repo === repo.path;
  const [expanded, setExpanded] = useState(isHere);
  const tasks = d?.notes?.tasks ?? [];
  const annotations = d?.notes?.annotations ?? [];
  const needs = tasks.filter((t) => taskNeed(t, annotations) === "needs").length;
  const pages = flattenPages(d?.tree?.pages ?? []);
  const areas = areaPages(d?.tree?.pages ?? [], d?.mapAreas ?? null);
  const currentPage = view.kind === "page" && isHere ? view.page : null;
  const shownTasks = [...tasks].sort((a, b) => (a.created < b.created ? 1 : -1)).slice(0, 6);

  const toggle = (next: boolean) => {
    setExpanded(next);
    if (next) open(repo.path);
  };

  const pageOfTask = (task: Task) => {
    const first = annotations.find((a) => task.inputs.includes(a.id));
    return first?.on.page ?? "";
  };

  return (
    <Collapsible open={expanded} onOpenChange={toggle} asChild>
      <SidebarMenuItem>
        <CollapsibleTrigger asChild>
          <SidebarMenuButton className="sb-repo" isActive={isHere && !expanded}>
            <ChevronRight className="sb-chev" data-open={expanded ? "yes" : "no"} />
            <span className="sb-repo-name">{repo.name}</span>
            <span className="sb-right">{repoMark(repo, tasks, needs)}</span>
          </SidebarMenuButton>
        </CollapsibleTrigger>
        <RepoActions repo={repo} />
        <CollapsibleContent>
          <SidebarMenuSub className="sb-sub">
            {repo.status === "starting" && (
              <li className="sb-note" aria-live="polite">
                Opening… <span className="sb-note-line">{(lines[repo.path] ?? []).slice(-1)[0] ?? repo.line}</span>
              </li>
            )}
            {repo.status === "failed" && (
              <li className="sb-note is-failed">
                <span>{repo.error || "CodeNib stopped"}</span>
                <button type="button" className="sb-link" onClick={() => restart(repo.path)}>
                  <RotateCw />
                  Try again
                </button>
              </li>
            )}
            {repo.status === "stopped" && !d?.tree && (
              <li className="sb-note">
                <button type="button" className="sb-link" onClick={() => open(repo.path)}>
                  Open
                </button>
              </li>
            )}
            {d?.error && <li className="sb-note is-failed">{d.error}</li>}
            {(d?.tree?.docs ?? []).map((doc) => (
              <SidebarMenuSubItem key={doc.id}>
                <SidebarMenuSubButton
                  isActive={currentPage === doc.id}
                  onClick={() => go({ kind: "page", repo: repo.path, page: doc.id })}
                >
                  <FileText />
                  <span>{doc.path === "README.md" ? "README" : doc.title}</span>
                </SidebarMenuSubButton>
              </SidebarMenuSubItem>
            ))}
            {pages.map((p) => (
              <SidebarMenuSubItem key={p.id}>
                <SidebarMenuSubButton
                  isActive={currentPage === p.id}
                  onClick={() => go({ kind: "page", repo: repo.path, page: p.id })}
                  style={{ paddingLeft: 8 + p.depth * 14 }}
                >
                  <span className="sb-page-pad" aria-hidden="true">
                    <span className="area-mark" style={{ background: hueFor(p.id, areas) }} />
                  </span>
                  <span>{p.title}</span>
                </SidebarMenuSubButton>
              </SidebarMenuSubItem>
            ))}
            {shownTasks.map((task) => (
              <SidebarMenuSubItem key={task.id}>
                <SidebarMenuSubButton
                  onClick={() =>
                    task.status === "needs_review"
                      ? go({ kind: "review", repo: repo.path, task: task.id })
                      : go({ kind: "page", repo: repo.path, page: pageOfTask(task) })
                  }
                  isActive={view.kind === "review" && view.task === task.id}
                >
                  <StatusMark status={taskNeed(task, annotations)} />
                  <span>{taskTitle(task, annotations)}</span>
                </SidebarMenuSubButton>
              </SidebarMenuSubItem>
            ))}
          </SidebarMenuSub>
        </CollapsibleContent>
      </SidebarMenuItem>
    </Collapsible>
  );
}

export function AppSidebar() {
  const { repos, data, view, go, chooseFolder, setPalette, setup } = useStore();
  const needs = repos.reduce((sum, repo) => {
    const d = data[repo.path];
    const annotations = d?.notes?.annotations ?? [];
    return sum + (d?.notes?.tasks ?? []).filter((t) => taskNeed(t, annotations) === "needs").length;
  }, 0);
  const agents = repos.flatMap((r) => data[r.path]?.notes?.agents ?? []);
  const claude = agents.find((a) => a.id === "claude-code");
  const running = repos.filter((r) => r.status === "ready").length;

  return (
    <Sidebar collapsible="none" className="app-sidebar">
      <SidebarHeader className="sb-head">
        <div className="titlebar-room" data-tauri-drag-region />
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button type="button" className="sb-brand">
              <NibMark size={16} />
              <span>CodeNib</span>
              <ChevronsUpDown className="sb-brand-chev" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="w-56">
            <DropdownMenuItem onSelect={() => void chooseFolder()}>Add a repository…</DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem disabled>{setup?.version ?? "CodeNib"}</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton onClick={() => setPalette({ open: true })}>
              <Search />
              <span>Search</span>
              <span className="sb-keys">
                <Kbd>⌘</Kbd>
                <Kbd>K</Kbd>
              </span>
            </SidebarMenuButton>
          </SidebarMenuItem>
          <SidebarMenuItem>
            <SidebarMenuButton onClick={() => go({ kind: "inbox" })} isActive={view.kind === "inbox"}>
              <Inbox />
              <span>Inbox</span>
              {needs > 0 && (
                <span className="sb-count" aria-label={`${needs} need you`}>
                  <StatusMark status="needs" />
                  {needs}
                </span>
              )}
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>

      <SidebarContent className="sb-content">
        <SidebarGroup>
          <SidebarGroupLabel className="sb-label">This Mac</SidebarGroupLabel>
          <SidebarMenu>
            {repos.map((repo) => (
              <RepoTree key={repo.path} repo={repo} />
            ))}
            <SidebarMenuItem>
              <SidebarMenuButton onClick={() => void chooseFolder()} className="sb-quiet">
                <Plus />
                <span>Add a repository</span>
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
        </SidebarGroup>
      </SidebarContent>

      <SidebarFooter className="sb-foot">
        <div className="sb-service">
          <FolderGit2 className="sb-service-icon" />
          <div>
            <div className="sb-service-line">
              {repos.length === 0 ? "No repositories yet" : `${running} of ${repos.length} open`}
            </div>
            <div className="sb-service-sub">
              {claude
                ? claude.available
                  ? "Claude Code found on this Mac"
                  : "Claude Code not found"
                : setup?.bundled
                  ? "CodeNib built into this app"
                  : (setup?.version ?? "")}
            </div>
          </div>
        </div>
      </SidebarFooter>
    </Sidebar>
  );
}
